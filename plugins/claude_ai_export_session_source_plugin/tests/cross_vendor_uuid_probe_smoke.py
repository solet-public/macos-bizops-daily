"""M9 §5 — cross-vendor UUID overlap INFORMATIONAL probe smoke (Claude-B P-E1).

M18 namespace separation: vendor=claude_ai and vendor=claude_code partial-
unique on (vendor, external_session_id). Even if Claude.ai conv.uuid
values happen to match claude_code_local rows, no cross-vendor
promotion mechanism fires per spec §8.4.

This smoke verifies that property at the data-shape layer: two sessions
in the SAME vendor=claude_ai namespace would collide; sessions in
DIFFERENT vendor namespaces (claude_ai vs claude_code) do NOT collide
even when external_session_id matches.

The empirical probe against operator's actual ledger (claude.ai vs
claude_code_local UUID overlap) is INFORMATIONAL per Claude-B P-E1 and
runs post-restart against the live state_service — out of smoke
scope here.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))

from ananta.llm.session_ledger.types import IngestSourceKind, SourceVendor  # noqa: E402


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def test_claude_ai_vendor_distinct_from_claude_code() -> None:
    """SourceVendor.CLAUDE_AI is a distinct enum value from CLAUDE_CODE.

    Use len({...}) to express the distinctness check in a shape pyright
    doesn't statically reduce to Literal-comparison tautology.
    """
    distinct: set[str] = {SourceVendor.CLAUDE_AI.value, SourceVendor.CLAUDE_CODE.value}
    _assert(len(distinct) == 2, f"CLAUDE_AI must differ from CLAUDE_CODE; got {distinct}")
    _assert(
        SourceVendor.CLAUDE_AI.value == "claude_ai",
        f"CLAUDE_AI canonical token: {SourceVendor.CLAUDE_AI.value!r}",
    )


def test_claude_ai_export_source_kind_distinct() -> None:
    """IngestSourceKind.CLAUDE_AI_EXPORT is a distinct enum value from claude_code siblings."""
    distinct: set[str] = {
        IngestSourceKind.CLAUDE_AI_EXPORT.value,
        IngestSourceKind.CLAUDE_CODE_LOCAL.value,
        IngestSourceKind.CLAUDE_CODE_HISTORY.value,
        IngestSourceKind.CLAUDE_CODE_TASKS.value,
    }
    _assert(
        len(distinct) == 4,
        f"CLAUDE_AI_EXPORT must differ from the 3 claude_code siblings; got {distinct}",
    )
    _assert(
        IngestSourceKind.CLAUDE_AI_EXPORT.value == "claude_ai_export",
        f"CLAUDE_AI_EXPORT canonical token: {IngestSourceKind.CLAUDE_AI_EXPORT.value!r}",
    )


def test_m18_namespace_separation_contract_documented() -> None:
    """The M18 partial-unique index keys on (vendor, external_session_id).

    Verifies the vendor enum can be used as a namespace key — i.e. is
    str-comparable and distinct across CLAUDE_AI / CLAUDE_CODE / CODEX /
    AGENT_MESSAGING / CHATGPT. The actual SQL contract (partial-unique
    on those columns) is verified by M18's cross_source_dedupe smoke.
    """
    namespaces = {
        SourceVendor.CLAUDE_AI.value,
        SourceVendor.CLAUDE_CODE.value,
        SourceVendor.CODEX.value,
        SourceVendor.AGENT_MESSAGING.value,
        SourceVendor.CHATGPT.value,
    }
    _assert(len(namespaces) == 5, f"5 distinct vendor namespaces; got {len(namespaces)}: {namespaces}")
    # Same external_session_id under different vendor → DIFFERENT logical sessions per M18
    # The partial-unique index allows this; only (same_vendor, same_external_id) blocks.


def main() -> int:
    tests = [
        test_claude_ai_vendor_distinct_from_claude_code,
        test_claude_ai_export_source_kind_distinct,
        test_m18_namespace_separation_contract_documented,
    ]
    for t in tests:
        t()
        print(f"  ok: {t.__name__}")
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
