#!/usr/bin/env python3
"""ChatGPT export ZIP member-detection smoke (Bug E 2026-06-13 amendment).

Run:

    .venv/bin/python3 ananta/tests/llm/session_ledger/chatgpt_export_shards_smoke.py

Background: OpenAI's ChatGPT data export shifted from a single
``conversations.json`` at the archive root to sharded
``conversations-NNN.json`` files (000, 001, ...). The 2026-06-11 operator
export carries 35 shards (000-034) + ``shared_conversations.json`` (which
is share-link metadata, NOT full transcripts). The vendor parser must
accept both shapes and skip the shared-metadata file.

This smoke verifies:

1. Legacy single-file ZIP → enumerator returns ``['conversations.json']``.
2. Sharded ZIP → enumerator returns shards in lexicographic order.
3. Mixed sharded + ``shared_conversations.json`` → shared file excluded.
4. ``load_conversations_member`` concatenates sharded payloads in order.
5. Unparseable shard → clear ValueError mentioning ``'is not valid JSON'``.
6. Non-array shard → clear ValueError mentioning ``'top-level must be a JSON array'``.
7. Empty archive (no conversation members) → ValueError mentioning ``'has neither'``.
"""

from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.llm.session_ledger.vendor.chatgpt_export import (  # noqa: E402
    LEGACY_CONVERSATIONS_MEMBER,
    SHARD_PATTERN,
    _enumerate_conversation_members,
    load_conversations_member,
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


def _make_zip(members: dict[str, str]) -> zipfile.ZipFile:
    """Build an in-memory ZIP with the given member-name → JSON-text map."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_STORED) as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    buf.seek(0)
    return zipfile.ZipFile(buf, mode="r")


def test_shard_pattern_matches_canonical_naming() -> None:
    _check(
        SHARD_PATTERN.match("conversations-000.json") is not None,
        "SHARD_PATTERN matches conversations-000.json",
    )
    _check(
        SHARD_PATTERN.match("conversations-034.json") is not None,
        "SHARD_PATTERN matches conversations-034.json",
    )
    _check(
        SHARD_PATTERN.match("conversations-9.json") is not None,
        "SHARD_PATTERN matches conversations-9.json (single digit)",
    )
    _check(
        SHARD_PATTERN.match("conversations.json") is None,
        "SHARD_PATTERN does NOT match legacy single-file name",
    )
    _check(
        SHARD_PATTERN.match("shared_conversations.json") is None,
        "SHARD_PATTERN does NOT match shared_conversations.json",
    )
    _check(
        SHARD_PATTERN.match("conversations-abc.json") is None,
        "SHARD_PATTERN does NOT match non-numeric suffix",
    )


def test_legacy_single_file_format() -> None:
    payload = json.dumps([{"conversation_id": "c1", "mapping": {}}])
    zf = _make_zip({LEGACY_CONVERSATIONS_MEMBER: payload})
    members = _enumerate_conversation_members(zf)
    _check(
        members == [LEGACY_CONVERSATIONS_MEMBER],
        f"legacy single-file → enumerator returns ['{LEGACY_CONVERSATIONS_MEMBER}']",
    )
    flat = load_conversations_member(zf)
    _check(
        flat == [{"conversation_id": "c1", "mapping": {}}],
        "legacy single-file → load_conversations_member returns the array",
    )


def test_sharded_format() -> None:
    shards = {
        "conversations-000.json": json.dumps([{"conversation_id": "c1"}]),
        "conversations-001.json": json.dumps([{"conversation_id": "c2"}]),
        "conversations-002.json": json.dumps([{"conversation_id": "c3"}]),
    }
    zf = _make_zip(shards)
    members = _enumerate_conversation_members(zf)
    _check(
        members == [
            "conversations-000.json",
            "conversations-001.json",
            "conversations-002.json",
        ],
        "sharded → enumerator returns shards in lexicographic order",
    )
    flat = load_conversations_member(zf)
    _check(
        flat == [
            {"conversation_id": "c1"},
            {"conversation_id": "c2"},
            {"conversation_id": "c3"},
        ],
        "sharded → load_conversations_member concatenates in order",
    )


def test_shared_conversations_excluded() -> None:
    zf = _make_zip({
        "conversations-000.json": json.dumps([{"conversation_id": "c1"}]),
        "shared_conversations.json": json.dumps(
            [{"conversation_id": "shared1", "is_anonymous": True}]
        ),
        "user.json": json.dumps({"id": "u1"}),
    })
    members = _enumerate_conversation_members(zf)
    _check(
        members == ["conversations-000.json"],
        "shared_conversations.json + user.json excluded; only shard returned",
    )


def test_legacy_takes_precedence_over_shards() -> None:
    """If somehow both formats are present, legacy wins (cheaper read)."""
    zf = _make_zip({
        LEGACY_CONVERSATIONS_MEMBER: json.dumps([{"conversation_id": "legacy"}]),
        "conversations-000.json": json.dumps([{"conversation_id": "shard0"}]),
    })
    members = _enumerate_conversation_members(zf)
    _check(
        members == [LEGACY_CONVERSATIONS_MEMBER],
        "both formats present → legacy single-file wins",
    )


def test_unparseable_shard_raises_value_error() -> None:
    zf = _make_zip({"conversations-000.json": "not valid json {"})
    raised: Exception | None = None
    try:
        load_conversations_member(zf)
    except ValueError as exc:
        raised = exc
    _check(
        raised is not None and "is not valid JSON" in str(raised),
        f"unparseable shard → ValueError mentions 'is not valid JSON' (got {raised!r})",
    )


def test_non_array_shard_raises_value_error() -> None:
    zf = _make_zip({"conversations-000.json": json.dumps({"not": "array"})})
    raised: Exception | None = None
    try:
        load_conversations_member(zf)
    except ValueError as exc:
        raised = exc
    _check(
        raised is not None and "top-level must be a JSON array" in str(raised),
        f"non-array shard → ValueError mentions 'top-level must be a JSON array' "
        f"(got {raised!r})",
    )


def test_neither_format_present_raises_value_error() -> None:
    zf = _make_zip({"chat.html": "<html></html>", "user.json": json.dumps({"id": "u1"})})
    raised: Exception | None = None
    try:
        _enumerate_conversation_members(zf)
    except ValueError as exc:
        raised = exc
    _check(
        raised is not None and "has neither" in str(raised),
        f"empty-of-conversations → ValueError mentions 'has neither' (got {raised!r})",
    )
    _check(
        raised is not None and "user.json" in str(raised),
        "error message lists actual json members for diagnostics",
    )


def test_sharded_order_with_gaps() -> None:
    """Lexicographic order holds even when shard numbers skip."""
    zf = _make_zip({
        "conversations-002.json": json.dumps([{"conversation_id": "c3"}]),
        "conversations-000.json": json.dumps([{"conversation_id": "c1"}]),
        "conversations-010.json": json.dumps([{"conversation_id": "c11"}]),
    })
    members = _enumerate_conversation_members(zf)
    _check(
        members == [
            "conversations-000.json",
            "conversations-002.json",
            "conversations-010.json",
        ],
        "sharded with gaps → lexicographic sort still chronological",
    )


def main() -> int:
    print("=== chatgpt_export_shards_smoke (Bug E 2026-06-13) ===")
    test_shard_pattern_matches_canonical_naming()
    test_legacy_single_file_format()
    test_sharded_format()
    test_shared_conversations_excluded()
    test_legacy_takes_precedence_over_shards()
    test_unparseable_shard_raises_value_error()
    test_non_array_shard_raises_value_error()
    test_neither_format_present_raises_value_error()
    test_sharded_order_with_gaps()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
