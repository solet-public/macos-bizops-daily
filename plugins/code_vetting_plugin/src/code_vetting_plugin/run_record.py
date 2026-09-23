"""run_record.py — the ``vetting_runs`` metrics row (F1 §3), Stream-O owned.

The bounded, metrics-only row persisted once per run to the ``vetting_runs``
state namespace. It deliberately carries NO findings: the finding trail lives as
its own records, and embedding it here would make the metrics trail its own leak
(F1 §3, design-brief §3.5). Findings are the *input* to the aggregates; the row
stores only the counts, the precision proxy, the coverage evidence, and the
tracked-debt burn-down.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .models import Dimension, Finding, Layer, Severity, Verdict


@dataclass(frozen=True, slots=True)
class RunTarget:
    """What was examined — the ``target`` sub-object of a vetting_runs row (F1 §3)."""

    repo: str
    ref: str
    scope: str
    # Foreign runs may add a bounded report-stage source-role partition.  None is
    # conditionally omitted so every existing RunTarget constructor and self-vet
    # payload remains byte-compatible.
    source_role_partition: Mapping[str, object] | None = None
    # Foreign runs may also carry a bounded declaration/topology fingerprint and
    # deterministic capability-routing needs. It rides the existing target JSON;
    # None is omitted so self-run bytes remain unchanged.
    technology_fingerprint: Mapping[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        target: dict[str, object] = {
            "repo": self.repo,
            "ref": self.ref,
            "scope": self.scope,
        }
        if self.source_role_partition is not None:
            target["source_role_partition"] = dict(self.source_role_partition)
        if self.technology_fingerprint is not None:
            target["technology_fingerprint"] = dict(self.technology_fingerprint)
        return target


@dataclass(frozen=True, slots=True)
class CoverageRecord:
    """Per-scanner coverage evidence — feeds ``files_examined`` + ``coverage_gaps``.

    A scanner that could not run (tool absent) records the gap here rather than
    silently passing, so "we reviewed it" is evidence, not a vibe (F1 §3).
    """

    scanner: str
    ran: bool
    files_examined: int
    gap_reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "scanner": self.scanner,
            "ran": self.ran,
            "files_examined": self.files_examined,
            "gap_reason": self.gap_reason,
        }


@dataclass(frozen=True, slots=True)
class AllowlistDelta:
    """Tracked-debt burn-down (F1 §3).

    ``totals`` is the current per-gate allowlist entry count. ``removed`` is the
    net entries removed since the prior run (positive = debt paid down, a unit of
    progress; negative = regression). ``removed`` stays ``None`` in v1 — cross-run
    diffing is a non-goal until ≥2 runs exist (F1 §4); v1 records the snapshot so
    the future diffing engine has the series.
    """

    totals: dict[str, int]
    removed: dict[str, int] | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "totals": dict(self.totals),
            "removed": None if self.removed is None else dict(self.removed),
        }


def counts_by_severity(findings: Sequence[Finding]) -> dict[str, int]:
    """Severity histogram over every finding (all layers) — the health trend."""
    counts = {severity.value: 0 for severity in Severity}
    for finding in findings:
        counts[finding.severity.value] += 1
    return counts


def counts_by_dimension(findings: Sequence[Finding]) -> dict[str, int]:
    """Dimension histogram over every finding (all layers), stable-keyed."""
    counts = {dimension.value: 0 for dimension in Dimension}
    for finding in findings:
        counts[finding.dimension.value] += 1
    return counts


def survival_rate(findings: Sequence[Finding]) -> float | None:
    """L2→L3 precision proxy: ``confirmed / (confirmed + refuted)`` (F1 §3).

    ``None`` when nothing was verified (no L3 pass) — a distinct signal from a
    rate of ``0.0`` (all critics refuted). A collapsing rate over runs means the
    critics are going noisy: the suite measuring itself.
    """
    confirmed = sum(1 for finding in findings if finding.verdict is Verdict.CONFIRMED)
    refuted = sum(1 for finding in findings if finding.verdict is Verdict.REFUTED)
    decided = confirmed + refuted
    if decided == 0:
        return None
    return confirmed / decided


def coverage_gaps(coverage: Sequence[CoverageRecord]) -> list[str]:
    """Subsystems/scanners that did not run — surfaced, not swallowed (F1 §3)."""
    return [f"{record.scanner}: {record.gap_reason}" for record in coverage if not record.ran and record.gap_reason is not None]


@dataclass(frozen=True, slots=True)
class RunMetrics:
    """The bounded, metrics-only ``vetting_runs`` row (F1 §3). No findings embedded."""

    run_id: str
    target: RunTarget
    started: str
    finished: str
    substrate: str
    layers_run: list[str]
    files_examined: list[CoverageRecord]
    counts_by_severity: dict[str, int]
    counts_by_dimension: dict[str, int]
    survival_rate: float | None
    coverage_gaps: list[str]
    allowlist_delta: AllowlistDelta
    # R8-1: the structural-metrics payload (per-run CCN/NLOC/params/nesting distribution +
    # aggregates + worst-offenders) so run-over-run trend queries work; None when the
    # structural_metrics scanner did not run (e.g. the quality-gate-only subset).
    structural_metrics: dict[str, object] | None = None
    # R9-A: the candidate-dead-symbols payload (60%-confidence family — L2 targeting evidence, the
    # test_reach pattern) so the AI-critic layer can consume it and trends accumulate; None when the
    # dead-code scanner did not run.
    dead_symbols: dict[str, object] | None = None
    # W3C-C3a: the run's severity-ranked markdown report, persisted so the get_vetting_run read-verb
    # serves it by run_id (the joseki's L2/L3 steps read the report + evidence without carrying a prior
    # step's runtime result); None for a metrics-only subset run that renders no report.
    report: str | None = None

    def to_dict(self) -> dict[str, object]:
        """The exact shape written to the ``vetting_runs`` namespace."""
        return {
            "run_id": self.run_id,
            "target": self.target.to_dict(),
            "started": self.started,
            "finished": self.finished,
            "substrate": self.substrate,
            "layers_run": list(self.layers_run),
            "files_examined": [record.to_dict() for record in self.files_examined],
            "counts_by_severity": dict(self.counts_by_severity),
            "counts_by_dimension": dict(self.counts_by_dimension),
            "survival_rate": self.survival_rate,
            "coverage_gaps": list(self.coverage_gaps),
            "allowlist_delta": self.allowlist_delta.to_dict(),
            "structural_metrics": self.structural_metrics,
            "dead_symbols": self.dead_symbols,
            "report": self.report,
        }


def build_run_metrics(
    *,
    run_id: str,
    target: RunTarget,
    started: str,
    finished: str,
    substrate: str,
    layers_run: Sequence[Layer],
    findings: Sequence[Finding],
    coverage: Sequence[CoverageRecord],
    allowlist_delta: AllowlistDelta,
    structural_metrics: dict[str, object] | None = None,
    dead_symbols: dict[str, object] | None = None,
    report: str | None = None,
) -> RunMetrics:
    """Assemble the metrics row from a run's findings + coverage (F1 §3).

    ``substrate`` records which inference engine reviewed/refuted this run
    (heuristic / local_inference / subscription) — provenance for the trail.
    ``structural_metrics`` (R8-1) / ``dead_symbols`` (R9-A) are the scanners' per-run payloads
    (already ``to_dict``-ed by the caller) for the trend + L2-targeting trail; None when not run.
    ``report`` (W3C-C3a) is the rendered markdown the ``get_vetting_run`` read-verb serves by run_id.
    """
    return RunMetrics(
        run_id=run_id,
        target=target,
        started=started,
        finished=finished,
        substrate=substrate,
        layers_run=[layer.value for layer in layers_run],
        files_examined=list(coverage),
        counts_by_severity=counts_by_severity(findings),
        counts_by_dimension=counts_by_dimension(findings),
        survival_rate=survival_rate(findings),
        coverage_gaps=coverage_gaps(coverage),
        allowlist_delta=allowlist_delta,
        structural_metrics=structural_metrics,
        dead_symbols=dead_symbols,
        report=report,
    )
