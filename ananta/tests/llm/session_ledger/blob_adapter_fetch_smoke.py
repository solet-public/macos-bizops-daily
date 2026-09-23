#!/usr/bin/env python3
"""Offline unit smoke for ``SessionLedgerBlobAdapter.fetch_event_text``.

The slice-2 ``external_id`` backfill recomputes an OFFLOADED row's content-key by
FETCHING the blob — so a wrong fetch silently diverges the dedup key. The live
backfill smoke proves the derivation GIVEN a working fetcher (stub); this RUNS
the real ``fetch_event_text`` + ``_decode_blob_text`` against the documented
``retrieve_blob`` envelope.

FIDELITY BOUNDARY: this exercises the envelope SHAPE that two real consumers read
identically — ``agent_messaging_plugin`` and ``default_thinking_plugin`` both take
``result["data"]["content"]`` and decode it as hex (filesystem provider) or bytes
(bootstrap). It does NOT run against the live S3 blob plugin; the envelope shape
is verified by those two consumers + the round-trip here, not end-to-end against
live blob storage.

Run from repo root:
    .venv/bin/python3 ananta/tests/llm/session_ledger/blob_adapter_fetch_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.llm.session_ledger.blob_adapter import (  # noqa: E402
    BlobAdapterError,
    SessionLedgerBlobAdapter,
)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


class _FakeBlobStorage:
    """Returns a fixed ``retrieve_blob`` envelope (the documented shape)."""

    def __init__(self, envelope: dict[str, Any]) -> None:
        self._envelope = envelope

    def retrieve_blob(self, blob_id: str) -> dict[str, Any]:  # noqa: ARG002
        return self._envelope


def _adapter(envelope: dict[str, Any]) -> SessionLedgerBlobAdapter:
    return SessionLedgerBlobAdapter(cast("Any", _FakeBlobStorage(envelope)))


def test_hex_content_decodes() -> None:
    # Filesystem provider: data.content is a hex string (one ASCII pair per byte).
    text = "hello — wörld\nwith a Ünicode tail 🌍"
    hexed = text.encode("utf-8").hex()
    adapter = _adapter({"action_status": "completed", "data": {"content": hexed}})
    _check(
        adapter.fetch_event_text("bmd_1") == text,
        "fetch_event_text decodes a HEX content envelope back to the exact UTF-8 text",
    )


def test_bytes_content_decodes() -> None:
    # Bootstrap provider: data.content is raw bytes.
    text = "raw bytes payload"
    adapter = _adapter({"action_status": "completed", "data": {"content": text.encode("utf-8")}})
    _check(
        adapter.fetch_event_text("bmd_2") == text,
        "fetch_event_text decodes a BYTES content envelope back to the exact UTF-8 text",
    )


def test_large_payload_round_trips() -> None:
    # The offload case: an oversized payload survives the hex round-trip intact.
    text = "z" * 250_000
    hexed = text.encode("utf-8").hex()
    adapter = _adapter({"action_status": "completed", "data": {"content": hexed}})
    got = adapter.fetch_event_text("bmd_big")
    _check(got == text, f"a 250 KB payload round-trips byte-exact (len {len(got)})")


def test_non_completed_status_fails_loud() -> None:
    adapter = _adapter({"action_status": "error", "error": "blob not found", "data": {}})
    try:
        adapter.fetch_event_text("bmd_missing")
    except BlobAdapterError as exc:
        _check("blob retrieval failed" in str(exc), "non-completed retrieve raises BlobAdapterError (fail-loud)")
        return
    _check(False, "expected BlobAdapterError on a non-completed retrieve")


def test_undecodable_content_fails_loud() -> None:
    # A None content (or any non-bytes/non-hex shape) must NOT silently yield a
    # wrong key — it raises, so the backfill stops rather than mis-stamp.
    adapter = _adapter({"action_status": "completed", "data": {"content": None}})
    try:
        adapter.fetch_event_text("bmd_bad")
    except BlobAdapterError as exc:
        _check("undecodable content" in str(exc), "undecodable content raises BlobAdapterError (fail-loud)")
        return
    _check(False, "expected BlobAdapterError on undecodable content")


def main() -> int:
    print("=== blob_adapter_fetch_smoke ===")
    for scenario in (
        test_hex_content_decodes,
        test_bytes_content_decodes,
        test_large_payload_round_trips,
        test_non_completed_status_fails_loud,
        test_undecodable_content_fails_loud,
    ):
        scenario()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
