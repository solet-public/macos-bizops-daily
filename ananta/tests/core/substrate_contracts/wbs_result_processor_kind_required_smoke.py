#!/usr/bin/env python3
"""Phase 0 freeze — every executable WBS step must declare a valid RPK (no pytest).

Protects contract (3) of the Phase 0 "freeze current contracts" work
(``workbench/2026-07-01_claude_coding_agent_substrate_architecture_and_planning_v2.md``
PART VI): an executable Joseki/WBS step (a step carrying at least one process
key, inside a WBS-execution document) MUST declare exactly one valid
``RESULT_PROCESSOR_KIND`` — ``inference`` or ``deterministic_continuation``.

Faithfulness: the primary assertions drive the real top-level entry
``core.plans.parser.parse`` (parser.py:662-663 gates the enforcement behind
``_is_joseki_wbs_execution_document`` and calls
``assert_executable_joseki_wbs_steps_declare_kind``). If that production call
site is deleted — i.e. WBS validation is bypassed — the MISSING assertion here
fails. The duplicate / invalid / forbidden cases are enforced per-step inside
``parse`` via ``_parse_result_processor_kind``.

The enforcement is header-gated: a document with no ``ACTIVE_WBS:`` /
``JOSEKI_KEY:`` / ``WORK_ITEM:`` header is not a WBS-execution document, so the
missing-RPK rule does not fire (covered here so the gate is explicit).

Offline: pure parser, no live solet / LM Studio / Postgres.

Run:
    .venv/bin/python3 \\
      ananta/tests/core/substrate_contracts/wbs_result_processor_kind_required_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))

from ananta.core.plans.parser import (  # noqa: E402
    assert_executable_joseki_wbs_steps_declare_kind,
    parse,
)
from ananta.core.result_processing.enums import ResultProcessorKind  # noqa: E402
from substrate_contract_fixtures import CONTRACT_WBS, GRAFT_KEY, Checker  # noqa: E402

_HDR = "ACTIVE_WBS: phase0-rpk\nWORK_ITEM: demo\n\n"


def _parse_error(text: str) -> str | None:
    """Return the ValueError message from parsing *text*, or None if it parsed."""
    try:
        parse(text)
    except ValueError as exc:
        return str(exc)
    return None


def test_valid_wbs_parses_with_kinds(c: Checker) -> None:
    plan = parse(CONTRACT_WBS)
    executable = [s for s in plan.steps if s.process_keys]
    c.check(
        len(executable) == 3,
        f"fixture WBS has 3 executable steps (got {len(executable)})",
    )
    c.check(
        all(
            s.result_processor_kind is ResultProcessorKind.DETERMINISTIC_CONTINUATION
            for s in executable
        ),
        "every executable fixture step declares deterministic_continuation",
    )


def test_missing_kind_on_executable_step_rejected(c: Checker) -> None:
    """The wired top-level path: parse() enforces RPK on executable WBS steps."""
    msg = _parse_error(_HDR + f"[ ] 1. Verify\n    a) Verify ({GRAFT_KEY})\n")
    c.check(
        msg is not None and msg.startswith("RESULT_PROCESSOR_KIND_MISSING:"),
        f"parse() rejects a missing RPK on an executable step (got {msg!r})",
    )


def test_duplicate_kind_rejected(c: Checker) -> None:
    msg = _parse_error(
        _HDR
        + "[ ] 1. Verify\n"
        + "    RESULT_PROCESSOR_KIND: inference\n"
        + "    RESULT_PROCESSOR_KIND: deterministic_continuation\n"
        + f"    a) Verify ({GRAFT_KEY})\n",
    )
    c.check(
        msg is not None and msg.startswith("RESULT_PROCESSOR_KIND_DUPLICATE:"),
        f"parse() rejects a duplicated RPK annotation (got {msg!r})",
    )


def test_invalid_kind_value_rejected(c: Checker) -> None:
    msg = _parse_error(
        _HDR
        + "[ ] 1. Verify\n"
        + "    RESULT_PROCESSOR_KIND: bogus_kind\n"
        + f"    a) Verify ({GRAFT_KEY})\n",
    )
    c.check(
        msg is not None and msg.startswith("RESULT_PROCESSOR_KIND_INVALID:"),
        f"parse() rejects an unknown RPK value (got {msg!r})",
    )


def test_bridge_delivery_kind_forbidden(c: Checker) -> None:
    """bridge_delivery is platform-set on direct MCP calls; never in WBS text."""
    msg = _parse_error(
        _HDR
        + "[ ] 1. Verify\n"
        + "    RESULT_PROCESSOR_KIND: bridge_delivery\n"
        + f"    a) Verify ({GRAFT_KEY})\n",
    )
    c.check(
        msg is not None and msg.startswith("RESULT_PROCESSOR_KIND_FORBIDDEN:"),
        f"parse() forbids bridge_delivery in WBS step text (got {msg!r})",
    )


def test_non_executable_step_exempt(c: Checker) -> None:
    """A step with no process key (e.g. Await USER) needs no RPK."""
    msg = _parse_error(_HDR + "[ ] 1. Await USER message\n")
    c.check(msg is None, f"non-executable step is exempt from RPK (got {msg!r})")


def test_missing_kind_not_enforced_without_wbs_header(c: Checker) -> None:
    """Enforcement is gated on the WBS-execution header; prove the gate."""
    msg = _parse_error(f"[ ] 1. Verify\n    a) Verify ({GRAFT_KEY})\n")
    c.check(
        msg is None,
        f"missing RPK is not enforced outside a WBS-execution document (got {msg!r})",
    )


def test_enforcement_helper_raises_on_missing(c: Checker) -> None:
    """The shared helper parse() AND projection.py both call (leaf coverage)."""
    plan = parse(f"[ ] 1. Verify\n    a) Verify ({GRAFT_KEY})\n")  # unheadered -> parses
    c.expect_raises(
        ValueError,
        "assert_executable_joseki_wbs_steps_declare_kind raises on a missing-RPK step",
        lambda: assert_executable_joseki_wbs_steps_declare_kind(plan.steps),
    )


def main() -> int:
    c = Checker("WBS RESULT_PROCESSOR_KIND required (Phase 0 contract 3)")
    print(f"=== {c.title} ===")
    test_valid_wbs_parses_with_kinds(c)
    test_missing_kind_on_executable_step_rejected(c)
    test_duplicate_kind_rejected(c)
    test_invalid_kind_value_rejected(c)
    test_bridge_delivery_kind_forbidden(c)
    test_non_executable_step_exempt(c)
    test_missing_kind_not_enforced_without_wbs_header(c)
    test_enforcement_helper_raises_on_missing(c)
    return c.summary()


if __name__ == "__main__":
    sys.exit(main())
