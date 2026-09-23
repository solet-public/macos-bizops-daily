"""Hermetic smoke for the test-coverage integration types (models.test_reach +
report ## Test Coverage table). No coverage tool needed — feeds a synthetic
harness artifact. House _check harness; run directly or via run_smokes.py."""

from __future__ import annotations

# ruff: noqa: E402
import sys
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from code_vetting_plugin.models import ContextProfile, Dimension, Layer, Severity
from code_vetting_plugin.report import ReportRenderer
from code_vetting_plugin.run_record import RunTarget
from code_vetting_plugin.test_coverage import (
    TestCoverageReport,
    build_test_reach_findings,
    render_test_coverage_section,
    zero_reach_targets,
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


_ARTIFACT = {
    "available": True,
    "gap_reason": None,
    "by_owner": [
        {"owner": "code_vetting_plugin", "files": 41, "executed": 444, "total": 2104,
         "zero_reach": ["plugins/code_vetting_plugin/src/code_vetting_plugin/l3_adapter.py"]},
        {"owner": "demo_canary_plugin", "files": 2, "executed": 0, "total": 26,
         "zero_reach": ["plugins/demo_canary_plugin/src/demo_canary_plugin/plugin.py"]},
    ],
}


def test_dimension_registered() -> None:
    _check(Dimension.TEST_REACH.value == "test_reach", "Dimension.TEST_REACH registered")


def test_roundtrip_and_render() -> None:
    report = TestCoverageReport.from_harness_artifact(_ARTIFACT)
    _check(report.available, "artifact parsed available")
    _check(len(report.by_owner) == 2, "two owners parsed")
    section = render_test_coverage_section(report)
    _check("## Test Coverage" in section, "renders ## Test Coverage header")
    _check("code_vetting_plugin" in section and "444" in section, "renders owner executed count")
    # No percentage anywhere — evidence, not a bar.
    _check("%" not in section, "no percentage in the section (evidence, not a %)")


def test_scanner_coverage_renamed() -> None:
    empty = ReportRenderer().render(
        run_id="vr-x", target=RunTarget(repo="example", ref="deadbeef", scope="s"),
        context_profile=ContextProfile.PRODUCTION, generated_at="t", findings=[], coverage=[],
    )
    _check("## Scanner Coverage" in empty, "scanner-coverage section renamed")
    _check("## Test Coverage" in empty, "test-coverage placeholder present when unpopulated")


def test_test_reach_finding_structural_not_percentage() -> None:
    report = TestCoverageReport.from_harness_artifact(_ARTIFACT)
    findings = build_test_reach_findings(
        run_id="vr-x", report=report, verb_owners=["demo_canary_plugin", "code_vetting_plugin"],
        context_profile=ContextProfile.PRODUCTION,
    )
    # demo_canary has verbs + 0 executed → a finding; code_vetting has 444 executed → none.
    _check(len(findings) == 1, "one test_reach finding (only the zero-executed verb owner)")
    _check(findings[0].dimension is Dimension.TEST_REACH, "finding dimension is test_reach")
    _check(findings[0].layer is Layer.L1_DETERMINISTIC, "finding is deterministic L1")
    _check(findings[0].severity is Severity.ADVISORY, "finding is advisory")
    _check("%" not in findings[0].evidence, "finding evidence names a fact, not a %")


def test_zero_reach_targets_for_critic() -> None:
    targets = zero_reach_targets(TestCoverageReport.from_harness_artifact(_ARTIFACT))
    _check("demo_canary_plugin" in targets, "zero-reach targets expose the critic aiming list")


def main() -> int:
    for test in (
        test_dimension_registered,
        test_roundtrip_and_render,
        test_scanner_coverage_renamed,
        test_test_reach_finding_structural_not_percentage,
        test_zero_reach_targets_for_critic,
    ):
        test()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
