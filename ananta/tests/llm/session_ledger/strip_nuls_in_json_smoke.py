#!/usr/bin/env python3
r"""NUL-byte sanitization for JSONB-bound payloads — Bug C 2026-06-13 amendment.

Run:

    .venv/bin/python3 ananta/tests/llm/session_ledger/strip_nuls_in_json_smoke.py

Background: the 2026-06-01 NUL-strip ratification (option B) carved out
``content_json`` on the (empirically false) premise that PostgreSQL JSONB
tolerates the ``\u0000`` Unicode escape that ``json.dumps`` emits for any
Python string containing ``\x00``. Postgres rejects the escape at parse
time with error 22P05 ``untranslatable_character`` ("``\u0000`` cannot be
converted to text"). Bug C surfaced when Bug A's fix unblocked the
importer and a codex tool-output capture with a NUL-bearing payload
reached ``append_event``.

This smoke verifies the new ``_strip_nuls_in_json`` helper applied at the
repository write seam (symmetric with ``_strip_nuls``):

1. ``_strip_nuls_in_json`` passes ``None`` through.
2. Top-level string with NUL is stripped.
3. NUL-free string passes through unchanged.
4. NUL is stripped from dict values at every nesting depth.
5. NUL is stripped from list/tuple elements.
6. Non-string scalar values (bool, int, float) pass through untouched.
7. Empty container shapes pass through unchanged.
8. After strip, ``json.dumps(result)`` contains no ``\u0000`` escape and
   no raw NUL byte — i.e. the round-trip is Postgres-safe.

No state-service stub, no transactional context — this is a pure helper
unit test that loads only the repository module.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.llm.session_ledger.shared import _strip_nuls_in_json  # noqa: E402

NUL = "\x00"
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


def test_none_passes_through() -> None:
    _check(_strip_nuls_in_json(None) is None, "None passes through")


def test_top_level_string_stripped() -> None:
    _check(
        _strip_nuls_in_json(f"abc{NUL}def") == "abcdef",
        "top-level string strips embedded NUL",
    )


def test_clean_string_unchanged() -> None:
    _check(
        _strip_nuls_in_json("clean text") == "clean text",
        "NUL-free string passes through unchanged",
    )


def test_dict_value_stripped() -> None:
    _check(
        _strip_nuls_in_json({"k": f"abc{NUL}"}) == {"k": "abc"},
        "dict value strips NUL",
    )


def test_nested_dict_and_list_stripped() -> None:
    payload = {
        "raw_content": [f"tool{NUL}output", {"inner": f"more{NUL}NUL"}],
        "meta": {"deep": {"deeper": f"deepest{NUL}"}},
    }
    expected = {
        "raw_content": ["tooloutput", {"inner": "moreNUL"}],
        "meta": {"deep": {"deeper": "deepest"}},
    }
    _check(
        _strip_nuls_in_json(payload) == expected,
        "nested dict + list values strip NUL recursively",
    )


def test_tuple_elements_stripped() -> None:
    payload = {"args": (f"a{NUL}", f"b{NUL}", "c")}
    result = _strip_nuls_in_json(payload)
    _check(
        result == {"args": ["a", "b", "c"]},
        "tuple elements strip NUL (and convert to list)",
    )


def test_non_string_scalars_passthrough() -> None:
    payload = {"a": True, "b": 42, "c": 3.14, "d": None, "e": False}
    _check(
        _strip_nuls_in_json(payload) == payload,
        "non-string scalars pass through untouched",
    )


def test_empty_containers_passthrough() -> None:
    _check(_strip_nuls_in_json({}) == {}, "empty dict passes through")
    _check(_strip_nuls_in_json([]) == [], "empty list passes through")
    _check(_strip_nuls_in_json("") == "", "empty string passes through")


def test_post_strip_json_dumps_is_postgres_safe() -> None:
    """After strip, json.dumps emits no \\u0000 escape and no raw NUL."""
    payload = {
        "raw_content": [
            f"tool output{NUL} discussing JSONB NUL handling",
            {"nested": f"more{NUL}{NUL}NULs"},
        ],
    }
    cleaned = _strip_nuls_in_json(payload)
    serialized = json.dumps(cleaned)
    _check(
        "\\u0000" not in serialized,
        "post-strip json.dumps emits no \\u0000 escape",
    )
    _check(
        NUL not in serialized,
        "post-strip json.dumps emits no raw NUL byte",
    )
    _check(
        "tool output discussing JSONB NUL handling" in serialized,
        "non-NUL content preserved in serialized JSON",
    )


def test_nul_only_string_becomes_empty() -> None:
    _check(_strip_nuls_in_json(NUL) == "", "NUL-only string becomes empty")
    _check(
        _strip_nuls_in_json({"k": NUL * 5}) == {"k": ""},
        "NUL-run-only value becomes empty string",
    )


def main() -> int:
    print("=== strip_nuls_in_json_smoke (Bug C 2026-06-13 amendment) ===")
    test_none_passes_through()
    test_top_level_string_stripped()
    test_clean_string_unchanged()
    test_dict_value_stripped()
    test_nested_dict_and_list_stripped()
    test_tuple_elements_stripped()
    test_non_string_scalars_passthrough()
    test_empty_containers_passthrough()
    test_post_strip_json_dumps_is_postgres_safe()
    test_nul_only_string_becomes_empty()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
