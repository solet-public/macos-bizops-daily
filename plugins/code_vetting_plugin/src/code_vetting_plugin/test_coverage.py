"""test_coverage.py — the vetting suite's TEST-coverage integration types.

Distinct from the src ``coverage.py`` / ``CoverageRecord`` (SCANNER coverage) and
from the ``tools/test_coverage/`` measurement harness (working data). This module
owns the bounded, persisted/rendered shape: the per-owner rollup that goes on the
``vetting_runs`` row's ``test_coverage`` key, the ``## Test Coverage`` markdown
section renderer, and the deterministic ``test_reach`` finding builder.

Evidence, never a gate (KB 22_testing/01 rejects coverage-% mandates): this
module renders raw executed/total counts and a zero-reach list, never a
percentage verdict, threshold, or bar.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .models import (
    ContextProfile,
    Dimension,
    Finding,
    Layer,
    Provenance,
    Severity,
)

# Cap the persisted zero-reach list per owner so the vetting_runs row stays
# bounded (the harness's full per-file map is NOT persisted — working data only).
_ZERO_REACH_CAP = 25


@dataclass(frozen=True, slots=True)
class OwnerTestCoverage:
    """One owner's executed/total rollup — visibility, not a verdict."""

    owner: str
    files: int
    executed: int
    total: int
    zero_reach: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "owner": self.owner,
            "files": self.files,
            "executed": self.executed,
            "total": self.total,
            "zero_reach": list(self.zero_reach[:_ZERO_REACH_CAP]),
            "zero_reach_total": len(self.zero_reach),
        }


@dataclass(frozen=True, slots=True)
class TestCoverageReport:
    """The bounded ``test_coverage`` artifact: availability + per-owner rollup."""

    available: bool
    gap_reason: str | None
    by_owner: tuple[OwnerTestCoverage, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "gap_reason": self.gap_reason,
            "by_owner": [owner.to_dict() for owner in self.by_owner],
        }

    @classmethod
    def from_harness_artifact(cls, artifact: dict[str, object]) -> TestCoverageReport:
        """Build from the tools/ harness JSON (``MeasureResult.to_dict``)."""
        if not artifact.get("available", False):
            reason = artifact.get("gap_reason")
            return cls(available=False, gap_reason=reason if isinstance(reason, str) else "unavailable")
        owners: list[OwnerTestCoverage] = []
        raw_owners = artifact.get("by_owner", [])
        if isinstance(raw_owners, list):
            for entry in raw_owners:
                if not isinstance(entry, dict):
                    continue
                zero = entry.get("zero_reach", [])
                owners.append(
                    OwnerTestCoverage(
                        owner=str(entry["owner"]),
                        files=int(entry["files"]),  # type: ignore[arg-type]
                        executed=int(entry["executed"]),  # type: ignore[arg-type]
                        total=int(entry["total"]),  # type: ignore[arg-type]
                        zero_reach=tuple(str(z) for z in zero) if isinstance(zero, list) else (),
                    )
                )
        return cls(available=True, gap_reason=None, by_owner=tuple(owners))


def render_test_coverage_section(report: TestCoverageReport | None) -> str:
    """The ``## Test Coverage`` markdown block (visibility only — no verdict)."""
    if report is None:
        return "## Test Coverage\n\n_No test-coverage evidence recorded (run the coverage run-profile)._"
    if not report.available:
        return f"## Test Coverage\n\n_Not measured — {report.gap_reason}._"
    lines = [
        "## Test Coverage",
        "",
        "_Lines of owned src executed by the unit + smoke suite. Evidence, not a bar._",
        "",
        "| owner | src files | lines executed | lines total | modules w/ 0 reach |",
        "| --- | --- | --- | --- | --- |",
    ]
    for owner in report.by_owner:
        lines.append(f"| {owner.owner} | {owner.files} | {owner.executed} | {owner.total} | {len(owner.zero_reach)} |")
    return "\n".join(lines)


def build_test_reach_findings(
    *,
    run_id: str,
    report: TestCoverageReport,
    verb_owners: Sequence[str],
    context_profile: ContextProfile,
) -> list[Finding]:
    """One deterministic ``test_reach`` finding per verb-exposing owner with zero executed src.

    ``verb_owners`` is the set of owners that expose ≥1 EDGE verb (supplied by the
    caller from the process registry — this module does not scan for verbs). A
    finding is a countable STRUCTURAL fact ("owner exposes verbs, 0 lines executed
    by any test"), never a coverage percentage. Advisory severity; the caller
    routes it per the zero-FP policy (recommended: NOT auto-promoted — see spec).
    """
    if not report.available:
        return []
    executed_by_owner = {owner.owner: owner.executed for owner in report.by_owner}
    findings: list[Finding] = []
    for owner in verb_owners:
        if executed_by_owner.get(owner, 0) == 0:
            findings.append(
                Finding.build(
                    run_id=run_id,
                    layer=Layer.L1_DETERMINISTIC,
                    dimension=Dimension.TEST_REACH,
                    severity=Severity.ADVISORY,
                    file=f"plugins/{owner}/src",
                    line=None,
                    constraint_violated="test_reach:verb-surface-zero-reach",
                    evidence=(
                        f"owner '{owner}' exposes ≥1 EDGE verb but the unit + smoke suite executed "
                        f"0 src lines under it — no test reaches this verb surface"
                    ),
                    fix_suggestion=(
                        f"add a hermetic smoke/unit exercising {owner}'s verb surface; "
                        "the test_adequacy critic consumes this owner's zero-reach list to name the specific untested path"
                    ),
                    provenance=Provenance(source="l1:test_reach"),
                    context_profile=context_profile,
                )
            )
    return findings


def zero_reach_targets(report: TestCoverageReport) -> dict[str, list[str]]:
    """Per-owner zero-reach module lists — the targeting evidence handed to the
    ``test_adequacy`` critic (which names the specific untested behavior + regression)."""
    return {owner.owner: list(owner.zero_reach) for owner in report.by_owner if owner.zero_reach}
