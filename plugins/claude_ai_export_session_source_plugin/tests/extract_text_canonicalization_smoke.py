"""M9 §5 — extract_text spec compliance smoke per Claude-B P-E2.

Verifies the exact fallback chain:
- str content → return as-is.
- list of text blocks → join with '\\n\\n'.
- list with mixed blocks (text + tool_use) → drop non-text blocks.
- empty content + non-empty msg['text'] → return msg['text'].
- all-empty (no content, no text) → return ''.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))

from ananta.llm.session_ledger.vendor.claude_ai_export import extract_text  # noqa: E402


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def test_str_content_returned_as_is() -> None:
    msg = {"content": "Just a string", "text": "short-form"}
    _assert(extract_text(msg) == "Just a string", extract_text(msg))


def test_list_of_text_blocks_joined() -> None:
    msg = {
        "content": [
            {"type": "text", "text": "Block 1"},
            {"type": "text", "text": "Block 2"},
            {"type": "text", "text": "Block 3"},
        ],
        "text": "ignored",
    }
    expected = "Block 1\n\nBlock 2\n\nBlock 3"
    _assert(extract_text(msg) == expected, extract_text(msg))


def test_mixed_blocks_drop_non_text() -> None:
    msg = {
        "content": [
            {"type": "text", "text": "Real text"},
            {"type": "tool_use", "name": "Bash", "input": {"cmd": "ls"}},
            {"type": "tool_result", "tool_use_id": "tu_1", "content": "output"},
            {"type": "text", "text": "Second real text"},
        ],
        "text": "ignored",
    }
    expected = "Real text\n\nSecond real text"
    _assert(extract_text(msg) == expected, extract_text(msg))


def test_empty_list_falls_back_to_text() -> None:
    msg = {"content": [], "text": "fallback short-form"}
    _assert(extract_text(msg) == "fallback short-form", extract_text(msg))


def test_list_all_non_text_falls_back_to_text() -> None:
    msg = {
        "content": [
            {"type": "tool_use", "name": "Bash"},
            {"type": "tool_result", "tool_use_id": "tu_1"},
        ],
        "text": "fallback",
    }
    _assert(extract_text(msg) == "fallback", extract_text(msg))


def test_empty_content_and_text_returns_empty() -> None:
    msg = {"content": None, "text": ""}
    _assert(extract_text(msg) == "", repr(extract_text(msg)))


def test_missing_content_and_text_returns_empty() -> None:
    msg: dict[str, object] = {}
    _assert(extract_text(msg) == "", repr(extract_text(msg)))


def test_block_without_text_key_skipped() -> None:
    msg = {
        "content": [
            {"type": "text"},  # no 'text' key
            {"type": "text", "text": "good"},
        ],
        "text": "fallback",
    }
    # Block with no 'text' key contributes empty string; non-empty block joins as-is
    _assert(extract_text(msg) == "good", extract_text(msg))


def main() -> int:
    tests = [
        test_str_content_returned_as_is,
        test_list_of_text_blocks_joined,
        test_mixed_blocks_drop_non_text,
        test_empty_list_falls_back_to_text,
        test_list_all_non_text_falls_back_to_text,
        test_empty_content_and_text_returns_empty,
        test_missing_content_and_text_returns_empty,
        test_block_without_text_key_skipped,
    ]
    for t in tests:
        t()
        print(f"  ok: {t.__name__}")
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
