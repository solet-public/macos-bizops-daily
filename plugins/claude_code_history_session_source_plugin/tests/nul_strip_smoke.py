"""M7 §1.5 — NUL-byte sanitization contract smoke.

NUL strip happens at the repository TEXT-write seam (`_strip_nuls` in
`ananta/src/ananta/llm/session_ledger/repository.py`) per the 2026-06-01
operator ruling captured in
`knowledge_bases/ananta_platform/19_session_ledger_02_nul_byte_sanitization_seam.md`.
Plugins emit raw text; the repository strips. This smoke verifies:

1. The plugin emits ``display`` text with NUL bytes intact (no premature
   strip at the source-plugin layer).
2. The repository's ``_strip_nuls`` actually removes them.
3. The normalize step preserves the bytes (strip is at INSERT, not in
   normalize) so a future caller could potentially route the raw text to
   a JSONB column that does NOT need stripping (the asymmetry documented
   in the KB article).
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0,
    str(_REPO_ROOT / "plugins" / "claude_code_history_session_source_plugin" / "src"),
)

from ananta.llm.session_ledger.shared import _strip_nuls  # noqa: E402
from ananta.llm.session_ledger.vendor import claude_code_history as v  # noqa: E402
from claude_code_history_session_source_plugin.plugin import (  # noqa: E402
    ClaudeCodeHistorySessionSourcePlugin,
)


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def test_parser_preserves_nul_bytes() -> None:
    text_with_nul = "hello\x00world"
    payload = (json.dumps({
        "display": text_with_nul,
        "timestamp": 1_700_000_000_000,
        "sessionId": "uuid-a",
        "project": "/p",
    }) + "\n").encode("utf-8")
    parsed = list(v.parse_file_from_offset(io.BytesIO(payload), 0))
    _assert(len(parsed) == 1, "one line expected")
    _assert(
        "\x00" in parsed[0].display,
        f"parser must NOT strip NULs (strip is repository-side); got {parsed[0].display!r}",
    )


def test_normalize_preserves_nul_bytes() -> None:
    text_with_nul = "alpha\x00beta"
    parsed = v._ParsedLine(  # type: ignore[reportPrivateUsage]
        external_session_id="uuid-a",
        display=text_with_nul,
        project="/p",
        event_at=__import__("datetime").datetime.fromtimestamp(0, tz=__import__("datetime").UTC),
        byte_offset=42,
    )
    raw = v.to_raw_event(parsed)
    plugin = ClaudeCodeHistorySessionSourcePlugin()
    normalized = plugin.normalize(raw)
    _assert(
        normalized.content_text is not None and "\x00" in normalized.content_text,
        f"normalize must preserve NULs (strip is at repository INSERT, not normalize); "
        f"got {normalized.content_text!r}",
    )


def test_repository_strip_nuls_removes_them() -> None:
    text_with_nul = "foo\x00bar\x00baz"
    cleaned = _strip_nuls(text_with_nul)
    _assert(
        cleaned == "foobarbaz",
        f"_strip_nuls should remove every NUL byte; got {cleaned!r}",
    )
    _assert(_strip_nuls(None) is None, "_strip_nuls(None) must return None")
    _assert(_strip_nuls("") == "", "_strip_nuls('') must return ''")
    _assert(
        _strip_nuls("no nuls here") == "no nuls here",
        "_strip_nuls leaves clean text alone",
    )


def main() -> int:
    tests = [
        test_parser_preserves_nul_bytes,
        test_normalize_preserves_nul_bytes,
        test_repository_strip_nuls_removes_them,
    ]
    for t in tests:
        t()
        print(f"  ok: {t.__name__}")
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
