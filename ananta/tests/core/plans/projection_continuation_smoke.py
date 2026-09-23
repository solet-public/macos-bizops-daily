#!/usr/bin/env python3
"""DEP-01 Phase-2a fix round — projection continuation-tail smoke (no pytest).

Recurrence-killer for the Rev-C blocker: the non-joseki ELSE branch of
``_append_phase_continuation`` (``PHASE_CONTINUATION`` phases 2 and 3)
still emitted the RETIRED ``create_work_breakdown_structure`` after the
joseki branch was rerouted — a projected plan step invoking a
de-registered process key fails at dispatch. This smoke projects every
continuation shape and asserts the emitted plan text NEVER references a
de-registered ``thinking_service`` verb:

* non-joseki phase-2 WBS → phase-3 continuation authors + registers by
  value (``register_authored_work_breakdown_structure``);
* non-joseki phase-3 WBS → phase-4 continuation likewise;
* non-joseki phase-1 WBS → phase-2 continuation stays on the
  deterministic ``generate_section_stem_wbs`` path;
* joseki-keyed WBS → the joseki continuation tail (already rerouted)
  stays clean too.

Blanket assertion on every projection: none of the six DEP-01-retired
verb keys appears anywhere in the emitted step text.

Offline: pure text projection, no live solet, no DB.

Run:
    .venv/bin/python3 ananta/tests/core/plans/projection_continuation_smoke.py
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))

from ananta.core.plans.projection import (  # noqa: E402
    project_wbs_to_plan_steps,
)

# The six thinking_service verbs retired by DEP-01 Phase-2a. A projected
# plan step referencing ANY of these invokes a de-registered process key.
# These keys exist here ONLY as negative fixtures — the smoke asserts
# projections never emit them — hence the line-scoped wint markers.
RETIRED_VERB_KEYS = (
    "service_interface::thinking_service::create_work_breakdown_structure",  # wint:negative-fixture
    "service_interface::thinking_service::create_work_breakdown_structure_outline",  # wint:negative-fixture
    "service_interface::thinking_service::create_wbs_work_item_detail",  # wint:negative-fixture
    "service_interface::thinking_service::create_joseki_work_breakdown_structure",  # wint:negative-fixture
    "service_interface::thinking_service::graft_work_breakdown_structure_detail_steps",  # wint:negative-fixture
    "service_interface::thinking_service::assemble_work_breakdown_structure",  # wint:negative-fixture
)

REGISTER_KEY = (
    "service_interface::thinking_service::register_authored_work_breakdown_structure"
)
GENERATE_KEY = "service_interface::thinking_service::generate_section_stem_wbs"
RECORD_STEP_KEY = (
    "service_interface::thinking_service::record_work_breakdown_structure_step_state"
)


def _wbs_fixture(phase_number: int, *, joseki_key: str | None = None) -> str:
    """Minimal valid WBS body for one phase with a single work item."""
    header = "# Work Breakdown Structure\n\n"
    if joseki_key is not None:
        header += f"JOSEKI_KEY: {joseki_key}\n\n"
    return header + (
        f"## Phase {phase_number}. Fixture Phase\n\n"
        f"### Work Item {phase_number}.1: Do the fixture work\n\n"
        f"[ ] 1. Run the fixture step\n"
        f"    RESULT_PROCESSOR_KIND: inference\n"
        f"    a) Search the knowledge base "
        f"(service_interface::knowledge_service::search)\n"
        f"[ ] 2. Record the step state\n"
        f"    RESULT_PROCESSOR_KIND: deterministic_continuation\n"
        f"    a) Record step state ({RECORD_STEP_KEY})\n"
    )


class Checker:
    """Minimal pass/fail accumulator."""

    def __init__(self, title: str) -> None:
        self.title = title
        self.passed = 0
        self.failed: list[str] = []

    def check(self, condition: object, label: str) -> None:
        if condition:
            self.passed += 1
            print(f"  PASS  {label}")
        else:
            self.failed.append(label)
            print(f"  FAIL  {label}")

    def summary(self) -> int:
        print(f"\n{self.passed} passed, {len(self.failed)} failed")
        for label in self.failed:
            print(f"  FAILED: {label}")
        return 1 if self.failed else 0


def _assert_no_retired_keys(c: Checker, projected: str, label: str) -> None:
    for key in RETIRED_VERB_KEYS:
        c.check(
            key not in projected,
            f"{label}: no de-registered verb {key.rsplit('::', 1)[-1]}",
        )


def test_phase2_continuation_registers_by_value(c: Checker) -> None:
    projected = project_wbs_to_plan_steps(_wbs_fixture(2))
    _assert_no_retired_keys(c, projected, "phase-2 projection")
    c.check(
        REGISTER_KEY in projected,
        "phase-3 continuation routes to register_authored_work_breakdown_structure",
    )
    c.check(
        "Author and register the Phase 3 Work Breakdown Structure" in projected,
        "phase-3 continuation step title says author-and-register",
    )


def test_phase3_continuation_registers_by_value(c: Checker) -> None:
    projected = project_wbs_to_plan_steps(_wbs_fixture(3))
    _assert_no_retired_keys(c, projected, "phase-3 projection")
    c.check(
        REGISTER_KEY in projected,
        "phase-4 continuation routes to register_authored_work_breakdown_structure",
    )


def test_phase1_continuation_stays_deterministic(c: Checker) -> None:
    projected = project_wbs_to_plan_steps(_wbs_fixture(1))
    _assert_no_retired_keys(c, projected, "phase-1 projection")
    c.check(
        GENERATE_KEY in projected,
        "phase-2 continuation stays on deterministic generate_section_stem_wbs",
    )
    c.check(
        REGISTER_KEY not in projected,
        "deterministic phase boundary does not also emit a register step",
    )


def test_joseki_continuation_stays_clean(c: Checker) -> None:
    projected = project_wbs_to_plan_steps(
        _wbs_fixture(2, joseki_key="fixture_stub_card"),
    )
    _assert_no_retired_keys(c, projected, "joseki projection")
    c.check(
        REGISTER_KEY in projected,
        "joseki continuation authors + registers the next fragment by value",
    )


def test_phase4_has_no_continuation(c: Checker) -> None:
    projected = project_wbs_to_plan_steps(_wbs_fixture(4))
    _assert_no_retired_keys(c, projected, "phase-4 projection")
    c.check(
        REGISTER_KEY not in projected and GENERATE_KEY not in projected,
        "terminal phase projects no next-phase WBS step",
    )


def main() -> int:
    c = Checker("projection continuation tails reference only live verbs")
    cases: list[Callable[[Checker], None]] = [
        test_phase2_continuation_registers_by_value,
        test_phase3_continuation_registers_by_value,
        test_phase1_continuation_stays_deterministic,
        test_joseki_continuation_stays_clean,
        test_phase4_has_no_continuation,
    ]
    for case in cases:
        print(f"\n{case.__name__}")
        case(c)
    return c.summary()


if __name__ == "__main__":
    sys.exit(main())
